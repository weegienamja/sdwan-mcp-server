# BlairScope for Windows

Use your existing Qwen3.5-4B GGUF to ask about the devices visible to your own Cisco account. The workstation runs on one Windows x64 laptop. It includes Python and a CPU build of llama.cpp. The GGUF stays in your Downloads folder.

The Windows app and Edge/Chrome extension are distributed together. [Download the Windows x64 ZIP](https://github.com/weegienamja/sdwan-mcp-server/releases/latest/download/SD-WAN-Workstation-Windows-x64.zip)

## Sign in with Edge or Chrome

1. Extract the Windows x64 ZIP into a permanent local folder. Run **Set up BlairScope.cmd** under your usual Windows account. Enter the work Manager HTTPS origin including any port, or press Enter to use DevNet only. Select your Qwen3.5-4B GGUF from Downloads. Setup registers the same local host for Edge and Chrome.
2. Open `edge://extensions` or `chrome://extensions` in your work browser profile. Where company policy permits, enable **Developer mode**, choose **Load unpacked** and select the kit’s **Edge extension** folder. In the browser’s Extensions menu, pin **BlairScope**. Managed installations may require IT distribution. When upgrading from the previous kit, run the new setup and load the new extension folder together.
3. In work mode, open the configured Manager, finish the usual SSO and MFA, and click **BlairScope** in that tab’s toolbar (or press **Alt+Shift+Q**). The full-height sidebar displays your capture beside the dashboard. The browser controls which side it docks on. In Chrome choose **Right** under **Settings → Appearance → Side panel** for a right-hand drawer.
4. Ask **Which sites have unreachable devices?** Use **Device detail** to inspect a router or **Refresh snapshot** for current data. Switching modes or active tabs clears the visible snapshot and conversation. Use **Saved chats** to reopen a thread for the selected environment. Click BlairScope on the desired Manager tab to capture again.

## Chat history

- Ask a follow-up in the same chat, such as **Are there any critical alarms there?** after asking about a particular site. Qwen receives the current capture and selected earlier exchanges. Chatting, opening a saved thread and starting a new chat make no additional Cisco requests.
- Choose **New chat** for an empty conversation. Its first answered question becomes its title in **Saved chats**. Select a saved chat to read it, including after restarting the browser or while Manager is unreachable. The Windows host must be available. Capture a snapshot from the signed-in Manager tab to ask a new question. Use **Show earlier messages** for longer transcripts. Each answer shows its original snapshot time.
- The full transcript stays available locally. To fit the GGUF context window, Qwen receives excerpts from up to eight previous exchanges. For longer threads BlairScope includes the latest six and two older exchanges selected by matching words in the new question. A refresh supplies current evidence while retaining the open thread.
- Questions, answers and capture times are saved in the extension’s local IndexedDB. Titles and turns are encrypted with a non-exportable AES key stored in that browser profile. History is partitioned by browser profile, exact Manager address and work/sandbox mode. Use separate browser profiles if you use different Cisco identities on the same Manager. In a private window chat lasts for the current panel session only.
- To remove a thread, open it and choose **Manage this chat → Delete this chat → Confirm deletion**. Removing the extension or its profile storage can also remove saved history. Chats from versions before 2.5 were held only in memory.

## DevNet sandbox

Switch on **Sandbox mode** and choose **Sign in to DevNet**. In Cisco’s catalogue, launch the SD-WAN sandbox and follow its reservation instructions, including VPN access if specified. Use the credentials Cisco provides on the lab Manager’s own sign-in page. After sign-in, click BlairScope in the lab Manager tab. **Open lab Manager** opens the configured lab address directly when you already have a reservation. Catalogue sign-in and Manager sign-in are separate steps. [Cisco’s reservation guide](https://developer.cisco.com/docs/sandbox/getting-started/)

The kit defaults to `https://sandbox-sdwan-2.cisco.com`. If the reservation lists a different Manager address, run the following in PowerShell inside the extracted kit, substituting its exact HTTPS origin. Complete the work Manager and GGUF prompts again, then reopen BlairScope.

```powershell
& '.\Set up BlairScope.cmd' --sandbox-manager 'https://lab-manager.example.com:8443'
```

BlairScope uses the browser’s existing same-origin access. Each mode accepts only its configured Manager origin. Inventory, alarms and device details use six fixed GET endpoints. Blocked reads and expired sign-ins appear in **Collection results**. [Cisco CSRF documentation](https://www.cisco.com/c/en/us/td/docs/routers/sdwan/configuration/sdwan-xe-gs-book/cisco-sd-wan-api-cross-site-request-forgery-prevention.html)

Only allowlisted evidence fields and source status travel to the Windows process through browser Native Messaging. Cisco session cookies, passwords, authorization headers and raw configurations stay out of the messages. Raw snapshots stay in memory. Chat transcripts are stored locally as described above. Qwen has no mechanism for requesting new Manager data. Collection is capped at 1,500 inventory rows, 250 alarms and 250 rows per connection table, with partial or oversized responses identified. [Microsoft Edge Native Messaging](https://learn.microsoft.com/en-us/microsoft-edge/extensions/developer-guide/native-messaging)

For deployment, setup writes `settings.json` and `edge-host.json` under `%LOCALAPPDATA%\SD-WAN Workstation` and registers `org.sdwan.workstation` in the current user’s `Software\Microsoft\Edge\NativeMessagingHosts` and `Software\Google\Chrome\NativeMessagingHosts` registry keys. The host permits only the packaged extension ID, `beapjnblokiomlppfhbidaaabofbolci`. IT should verify the extension identity when preparing a managed or store distribution. Removing the extension, both registrations and its settings folder removes the integration.

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
