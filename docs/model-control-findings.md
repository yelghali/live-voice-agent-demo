# Which model does a Foundry agent use in voice mode, and what can you control?

Answers below are from running [`scripts/probe_model_control.py`](../scripts/probe_model_control.py)
and [`scripts/probe_voice_matrix.py`](../scripts/probe_voice_matrix.py) against
`fdy-sa33b5nih2ogs` (project `proj-chatbot-gr`, francecentral) on 13 August 2026,
not from reading the documentation.

---

## Short answer

**Voice Live agent mode does not let you choose the model.** The agent's own chat
deployment is the brain, fixed at the moment you create the agent version. Audio is
always **cascaded** — Azure Speech to text, then that chat model, then Azure text to
speech. It is not native speech-to-speech.

Your `gpt-realtime-1.5` deployment therefore **cannot back a Foundry agent**. It is
reachable only in *direct model* mode, where there is no server-side agent at all.

The `model` and `profile` query parameters are **accepted and silently discarded**
in agent mode, which makes this easy to get wrong: the session connects, so it looks
like it worked.

---

## The three models in every Voice Live session

Voice Live is not one model. It is three, chosen independently.

| Layer | Set by | Agent mode | Direct model mode |
|---|---|---|---|
| **Brain** (LLM) | `PromptAgentDefinition(model=...)` / `?model=` | Agent's chat deployment. **No client override.** | `?model=` |
| **Ears** (STT) | `session.input_audio_transcription.model` | `azure-speech` or `mai-transcribe` | plus `whisper-1`, `gpt-4o-transcribe`, … when the brain is a realtime model |
| **Mouth** (TTS) | `session.voice` | Azure voices only | Azure voices, or the model's native voice |

The Foundry portal's **"Generative AI Model"** dropdown that shows *GPT Realtime 1.5*
belongs to the **direct-model** playground (`Azure-Speech-Voice-Live/playground`).
It is not the agent playground. In the agent playground the voice pane exposes only
voice, VAD, temperature and speed — because the model is not yours to pick there.

---

## Evidence

### Agent mode ignores `?model=` and `?profile=`

A session connecting proves nothing on its own, so each real experiment has a
deliberately invalid control alongside it.

| # | Parameters | Result | Reported voice |
|---|---|---|---|
| 1 | `model=gpt-realtime-1.5` | connected | `alloy` |
| 2 | `model=gpt-realtime-1.5` + `profile=byom-azure-openai-realtime` | connected | `alloy` |
| 3 | `agent_name` + `project_name` | connected | `en-US-AvaMultilingualNeural` |
| 4 | agent + `model=gpt-realtime-1.5` | connected | `en-US-AvaMultilingualNeural` |
| 5 | agent + `model` + `profile=byom-...` | connected | `en-US-AvaMultilingualNeural` |
| **6** | agent + **`model=this-model-does-not-exist-xyz`** | **connected** | `en-US-AvaMultilingualNeural` |
| **7** | agent + **`profile=byom-not-a-real-profile-xyz`** | **connected** | `en-US-AvaMultilingualNeural` |
| **8** | agent + voice `{type: azure-realtime-native}` | **rejected** — `Only Azure voice is supported` | — |

Read rows 6 and 7 first. A model name that cannot exist connects happily, and so does
a BYOM profile that cannot exist. Both parameters are being dropped. That makes rows
4 and 5 false positives — they did not demonstrate control, they demonstrated that
the service ignored the request.

Row 8 independently confirms the audio path. `azure-realtime-native` voices only work
when a native speech-to-speech model is generating the audio. Agent mode rejects them
with the same error a plain `gpt-4o-mini` session gives, which places agent mode
firmly on the cascaded Azure TTS path.

The reported voice corroborates it: direct realtime sessions come back as `alloy`
(the model's own voice), while every agent session comes back as the Azure TTS voice
stored in the agent's metadata.

### Your own deployment does work — in direct-model mode

Experiment 2 succeeded. `profile=byom-azure-openai-realtime&model=gpt-realtime-1.5`
routes to *your* Data Zone Standard deployment, so you keep EU processing, your
content-filter configuration, and your quota.

No extra role assignment was needed. The docs require granting the resource's
managed identity `Foundry User` for `byom-azure-openai-chat-completion` and
`byom-foundry-anthropic-messages`; the realtime profile did not need it here, and the
resource already had a system-assigned identity.

### What you *can* control in agent mode

Everything except the brain, via the agent metadata key
`microsoft.voice-live.configuration` (chunked at 512 characters per value — see
`chunk_config` in [`agent/_common.py`](../agent/_common.py)). Verified round-tripping
intact by `agent/create_rfp_agent.py`:

```json
{"session": {
  "voice": {"name": "en-US-AvaMultilingualNeural", "type": "azure-standard"},
  "input_audio_transcription": {"model": "azure-speech"},
  "turn_detection": {"type": "azure_semantic_vad_multilingual",
                     "remove_filler_words": true, "auto_truncate": true},
  "input_audio_noise_reduction": {"type": "azure_deep_noise_suppression"},
  "input_audio_echo_cancellation": {"type": "server_echo_cancellation"}}}
```

To change the brain you create a **new agent version** with a different `model`.
That is the only lever, and `agent_version` is how you pin it.

### How to see what a live session resolved to

`session.updated` reports the agent and the voice. It does **not** report the LLM
deployment name — pin `agent_version` if you need that to be deterministic.

```
SessionID       : sess_1iu0UIOmse9nxQES8aIoUk
Agent Name      : rfp-voice-agent
Voice Name      : en-US-AvaMultilingualNeural
Voice Type      : azure-standard
```

`agent/voice_live_agent_client.py` writes this to `logs/<timestamp>_conversation.log`
on every run.

---

## So what *is* "the voice model" in agent mode?

There isn't one. There are three separate models, and the audio ones are Azure
Speech, not a realtime model. From `scripts/probe_agent_session.py`:

| Stage | Component | Value |
|---|---|---|
| **Ears** | speech to text | `azure-speech` (Azure Speech STT, 24 kHz) |
| **Brain** | LLM | `gpt-5` — from the agent version, **not reported in the session** |
| **Mouth** | text to speech | `en-US-AvaMultilingualNeural`, type `azure-standard` |
| — | turn detection | `azure_semantic_vad_multilingual` |
| — | noise / echo | `azure_deep_noise_suppression`, `server_echo_cancellation` |

So "the voice model" is an **Azure Neural TTS voice**. It is not GPT Realtime, and no
realtime or audio model appears anywhere in the session.

The single most telling field is `session.model`:

```json
{
  "id": "sess_42ZBmG4US2vQdz0y9xf9CA",
  "model": "rfp-voice-agent",          // <- the AGENT occupies the model slot
  "voice": {"name": "en-US-AvaMultilingualNeural", "type": "azure-standard"},
  "input_audio_transcription": {"model": "azure-speech"},
  "agent": {"type": "agent", "name": "rfp-voice-agent"}
}
```

In direct-model mode that field holds a model name. In agent mode it holds the
*agent name* — the slot is already taken, which is precisely why passing `?model=`
has nothing to override.

### Which of those can you change?

| Component | Changeable? | How |
|---|---|---|
| TTS voice | yes | agent metadata, or `session.update` at runtime |
| Voice style / rate / temperature | yes | same `voice` object |
| STT model | yes | `azure-speech` or `mai-transcribe` |
| Turn detection, noise, echo | yes | same metadata block |
| **LLM** | only by creating a new agent version | `PromptAgentDefinition(model=...)` |
| **Swapping in a realtime model** | **no** | not available in agent mode at all |

---


### Region: francecentral is better than documented

The docs list HD voices for southeastasia, centralindia, swedencentral, westeurope,
eastus, eastus2 and westus2 only. francecentral is absent — but every voice tested
was accepted and echoed back unchanged:

| Voice | Result |
|---|---|
| `en-US-AvaMultilingualNeural` | PASS |
| `en-US-AvaNeural` | PASS |
| `en-US-Ava:DragonHDLatestNeural` | PASS (despite the region list) |
| `en-US-Harper:MAI-Voice-2-Flash` | PASS |
| `ava` (`azure-realtime-native`) | FAIL — needs the `azure-realtime` model |

Models reachable from this resource: `gpt-realtime-1.5`, `gpt-realtime`,
`gpt-realtime-mini`, `gpt-4o-mini`, `gpt-5`, `azure-realtime` all connected.
`phi4-mm-realtime` did not.

The default here is still `en-US-AvaMultilingualNeural`, since HD support in a region
Microsoft does not document is not something to depend on in production.

---

## What this means for the RFP agent

Both tracks can do VoiceRAG **and** MCP. The real difference is who runs each piece.

| | Track A — agent mode | Track B — direct model + BYOM |
|---|---|---|
| Brain | agent's chat deployment (`gpt-5`) | **your `gpt-realtime-1.5`** |
| Audio | cascaded STT → LLM → TTS | native speech-to-speech |
| RFP grounding | **File Search, managed by Foundry** | `search_rfp` function, run by your backend |
| MCP | managed by Foundry Agent Service | **native to Voice Live**, executed by the service |
| Conversation history | Foundry threads + tracing | your problem |
| Model choice | fixed by agent version | yours |
| Who holds the socket | the client | your backend |

### MCP works in both — this corrects an earlier assumption

MCP is **not** exclusive to agent mode. Declaring an `mcp` tool in `session.update`
works in direct-model mode, and Voice Live connects to the server, lists its tools,
and invokes them itself. Verified: `mcp_list_tools.completed`, then
`response.mcp_call.*`, then a grounded spoken answer citing three BYOM profiles.

Two caveats that cost real debugging time:

1. **You must call `response.create()` after `response.mcp_call.completed`.** Voice
   Live executes the tool but does not then speak the result. Miss this and the turn
   dies silently — the model never says anything and the call looks hung. Wait until
   *all* in-flight MCP calls finish, or the model answers on partial results.

2. **Tool calls can also arrive as ordinary `function_call` items** naming an MCP
   tool, which the client is expected to execute. If nothing answers them, the
   response completes with zero audio. `backend/tools.py` therefore proxies any
   unrecognised tool name to the MCP server as a safety net.

Retrieval is the genuine asymmetry: agent mode gets **managed File Search** with no
retrieval code at all, while direct-model mode has no equivalent, so RAG stays a
function tool your backend implements.

**Choose Track A** when you want Foundry to own orchestration — retrieval, threads,
tracing, versioning — and you can accept cascaded latency. Least code.

**Choose Track B** when latency or model/data-residency control dominates, and you
can host a small backend for retrieval. Annex D of the sample tender asks for turn
latency under 1.2 s at the 95th percentile, which is the kind of target that pushes
you here.

Both give you MCP. Only Track A gives you managed RAG. Only Track B gives you the
model.

---

## Networking: who dials out, and can it be private?

This is the constraint most likely to decide the architecture in a regulated tender,
and it cuts the opposite way from everything above.

### Who originates the connection

| | Track A — agent mode | Track B — direct model |
|---|---|---|
| MCP server is called by | Foundry Agent Service | **Voice Live itself** |
| Retrieval is called by | Foundry (File Search / AI Search) | **your backend** |

Proved for Track B by pointing the MCP tool at `http://localhost:9999/mcp` — a server
only reachable from this laptop. The session accepted the tool, then:

```
session.updated (tools accepted)
mcp_list_tools.in_progress
mcp_list_tools.failed
```

Voice Live is dialling out, not the client. The tool declaration is accepted
regardless, so a bad URL fails at discovery rather than at configuration.

### What that means for private networking

**MCP.** In Track B the MCP server must be reachable from the Voice Live service —
in practice, a public endpoint. The `mcp` tool takes only `server_url`, `headers`,
`authorization`, `allowed_tools` and `require_approval`; there is no VNet or private
link parameter, and the troubleshooting guidance is to confirm the server is
"accessible from Azure's network". Authentication can be locked down; network
exposure cannot.

Track A does support private MCP, and it is documented explicitly:

| Agent tool | Supported when network-isolated | Traffic flow |
|---|---|---|
| MCP Tool (Private MCP) | yes | through your VNet subnet |
| Azure AI Search | yes | through private endpoint |
| File Search | yes | through private endpoint |
| Function Calling | yes | Microsoft backbone |
| Bing / Websearch / SharePoint | yes | **public endpoint** |

Private MCP requires **Standard agent setup with VNet injection** (BYO VNet, subnet
delegated to `Microsoft.App/environments`, /27 or larger). Basic setup does not
support it.

**Retrieval.** Track B is the *stronger* position here, not the weaker one. Because
your backend performs the search, Azure AI Search or the vector store never has to be
reachable by any Microsoft service — put the backend in the VNet, give the search
service a private endpoint, and the retrieval path never leaves your network. Track A
also supports private AI Search, but only with the Standard + VNet setup above.

Note the residual flow either way: the *retrieved text* is sent to Voice Live as tool
output so the model can speak it. Private networking protects the search service, not
the content of the answer.

### The uncomfortable summary

- Need **private MCP**? Only Track A, and only with Standard setup + VNet injection.
- Need **private retrieval** with minimal infrastructure? Track B — your backend owns it.
- Need private MCP **and** your own realtime model? Not available today. That is the
  same wall as the model-control finding, arriving from a different direction.

Not verified here: whether the Voice Live WebSocket itself is reachable over a Foundry
private endpoint with public network access disabled. Worth testing before promising
a fully private Track B deployment.

---

## Head to head: three ways to build the same VoiceRAG agent

Three realistic shapes, all running the same RFP scenario in this repo:

* **A — Agent (private)** — Foundry Agent Service, Standard setup, VNet injection,
  private endpoints on Storage / AI Search / Cosmos DB, driven through Voice Live
  agent mode.
* **B — Direct Voice Live** — direct-model mode, BYOM to your own `gpt-realtime-1.5`,
  with a backend you host (this repo's `backend/`).
* **C — Native Azure OpenAI Realtime** — the same deployment, reached at
  `wss://<resource>.openai.azure.com/openai/realtime`, with **no Voice Live in the
  path**. Microsoft positions Voice Live as "designed for compatibility with the Azure
  OpenAI Realtime API", with its own features "optional and additive" — so C is the
  baseline those additions sit on top of.

| | A · Agent mode, private Standard | B · Direct Voice Live + BYOM | C · Native AOAI Realtime |
|---|---|---|---|
| **Model choice** | Fixed by agent version; must be a **chat** deployment | **Yours** — any deployment, incl. realtime, via `profile=byom-…` | **Yours** — whatever you deployed |
| **Audio path** | **Cascaded** Azure STT → LLM → Azure TTS | **Native speech-to-speech** | **Native speech-to-speech** |
| **Median first audio (no tool)** | 3.9 s on `gpt-5`, **1.4 s** on `gpt-4o-mini` | **0.41 s** | **0.36 s** |
| **Median first audio (one retrieval)** | 13.6 s on `gpt-5`, **3.1 s** on `gpt-4o-mini` | **1.96 s** | **1.28 s** |
| **Voices** | 600+ Azure Neural, HD, MAI, **custom voice** | Same, on top of a realtime model | ❌ model-native only (`alloy`, `echo`, …) |
| **Turn detection** | `azure_semantic_vad`, filler-word removal, EOU model | Same | ❌ `server_vad` / `semantic_vad` only |
| **Noise suppression / echo cancellation** | ✅ server-side | ✅ server-side | ❌ build it yourself |
| **Avatar, visemes, word timestamps** | ✅ | ✅ | ❌ |
| **Data residency of inference** | Whatever the agent's deployment is | **Your deployment's** (e.g. Data Zone EU) | **Your deployment's** |
| **Extra residency surface** | Azure STT + TTS legs, in-region by contract | Azure TTS leg, in-region by contract | **None** — one hop, one deployment |
| **Private network** | ✅ tools and data through your VNet; service still dials the socket | Backend can be private; the Voice Live socket is a public egress | **Private endpoint on the Azure OpenAI resource** is the mature, documented path |
| **Content filter** | Foundry default unless BYO | Yours, incl. async filtering for latency | Yours |
| **Retrieval (RAG)** | **Managed File Search or AI Search tool** | You implement; backend queries AI Search / vector store | You implement, same as B |
| **Retrieval privacy** | Private endpoint, needs Standard + VNet | **Strongest** — search reachable only by your backend | **Strongest** |
| **MCP, public server** | Native, executed by Foundry | Native, executed by Voice Live | ❌ no native MCP — function call only |
| **MCP, private server** | ✅ native, through your VNet subnet | ❌ not native — **use a function call** (proven below) | ✅ trivially — your backend is the MCP client |
| **Who executes tools** | Foundry | Voice Live (MCP) or your backend (functions) | **Always your backend** |
| **Tool approval flow** | `require_approval` + `mcp_approval_request` | Same, for native MCP | You build it |
| **Threads / history / tracing** | **Built in** | You build it | You build it |
| **Versioning / rollback** | `agent_version` pinning | Your deploy pipeline | Your deploy pipeline |
| **Interim "let me look that up"** | ✅ `interim_response` | ❌ unsupported on realtime pipelines | ❌ |
| **Infra you run** | None beyond the Standard BYO resources | A backend service (+ VNet if private) | A backend service |
| **Setup floor** | **Standard setup, BYO VNet, /27 delegated subnet, 3 private endpoints** | Foundry resource + a deployment | Azure OpenAI deployment |
| **Auth to the service** | **Entra only** | Entra or API key | Entra or API key |
| **Billing** | Voice Live rate (Pro/Basic/Lite) + agent | Voice Live rate + your deployment | **Your deployment only** |

### Measured latency

`python scripts/bench_latency.py --runs 3` — three measured turns per question after a
discarded warm-up, medians reported, single machine, `francecentral`.

| Track | chit-chat, first audio | chit-chat, complete | retrieval, first audio | retrieval, complete | tool |
|---|---|---|---|---|---|
| A · agent mode, `gpt-5` | 3 920 ms | 3 923 ms | 13 555 ms | 15 325 ms | server-side |
| A · agent mode, `gpt-4o-mini` | 1 415 ms | 1 421 ms | 3 141 ms | 4 316 ms | server-side |
| B · Voice Live direct + BYOM | **406 ms** | 416 ms | 1 958 ms | 3 725 ms | 669 ms |
| C · native AOAI Realtime | **363 ms** | 387 ms | **1 279 ms** | **2 924 ms** | 559 ms |

Three things this settles:

1. **Cascaded is a different latency class.** Even on a fast chat model, agent mode
   needs ~1.4 s before the first syllable, because the LLM has to finish enough text
   for TTS to start. The realtime tracks start speaking in under half a second. Annex
   D's P-02 (<1.5 s first response) is already at the edge for track A **before**
   adding the speech-to-text hop.
2. **The `gpt-5` number is a model choice, not a cascade tax.** A control agent on
   `gpt-4o-mini` cut retrieval latency from 13.6 s to 3.1 s. But that is the point:
   agent mode makes you pick a text model and pay its *entire* think time before a
   word is spoken, whereas a realtime model talks while it thinks.
3. **Voice Live costs roughly 40–700 ms over the raw API.** B and C run the *same
   deployment*; the delta is the Voice Live layer plus Azure TTS instead of native
   audio. That is the price of Azure voices, semantic VAD, noise suppression and
   native MCP. Whether it is worth it is a product decision, not a technical one.

**Read the numbers honestly.** The user turn is injected as **text**, so the
speech-to-text hop is excluded from every track. Tracks B and C barely have one — the
model consumes audio directly — but track A does STT before the LLM starts, so these
numbers *flatter the cascaded track*. Its real microphone-to-audio latency is higher
than shown. The comparison is still fair for the LLM + TTS + tool portion, which is
where the architectural difference actually lives.

### Private MCP without native MCP: use a function call

If the MCP server is private, direct-model mode does not lose MCP — it moves the MCP
client into your backend. `scripts/probe_private_mcp_via_function.py` proves this with
a real MCP server bound to loopback and a secret that exists nowhere else:

```
A. Native MCP tool   -> mcp_list_tools.failed  (Azure cannot see 127.0.0.1)
B. Function tool     -> model called get_supplier_policy({"query": "..."})
                        [private server] tools/call get_supplier_policy
   spoken: "Policy code SUP-2026-QX41 requires that the transition manager have at
            least ten years of contact centre migration experience..."
```

The model spoke a value that only the private server holds, so the data really did
travel: model → Voice Live → your backend → private MCP server → back. The service
never opened a connection to the private network.

```
declare:  {"type": "function", "name": "get_supplier_policy", ...}
on call:  McpProxy(private_url).call(name, args)   # backend is the MCP client
return:   FunctionCallOutputItem(call_id=..., output=...)
```

What you give up versus native MCP: automatic tool discovery (you declare the schema
yourself), the built-in approval flow, and the service-side retry and timeout
handling. What you gain: the server never has to be publicly reachable, and you can
log and police every call.

### Choosing

- **Annex D's 1.2 s P95 turn latency dominates** → B or C. Track A cannot get there
  through a cascade, whatever model you pick.
- **You need Azure voices, custom voice, semantic VAD or an avatar** → B. That is the
  entire reason to accept Voice Live's ~40–700 ms over the raw API.
- **You want the shortest possible path and own everything else** → C. One hop, one
  deployment, one residency story, and the lowest measured latency — but you build
  VAD, barge-in polish and noise handling yourself, and you live with `alloy`.
- **Least code, governance, auditability dominates** → A, private Standard setup.
- **Private MCP with native tool semantics** → A is the only option.
- **Private MCP but you need your own realtime model** → B or C, with the MCP client
  in your backend as a function tool.

---

## Authenticating to MCP servers and to retrieval

Documentation review, not implemented in this repo. The pattern differs sharply by
track, because a different party is holding the credential.

### To an MCP server

| Track | Who is the MCP client | How it authenticates |
|---|---|---|
| A · agent mode | Foundry Agent Service | A **project connection** (`project_connection_id`). Create with `azd ai connection create --kind remote-tool --auth-type …`, where the type is one of `none`, `custom-keys` (e.g. `Authorization=Bearer <PAT>`), `oauth2` (authorization URL, token URL, client id/secret, scopes), `user-entra-token` (plus `--audience`, on-behalf-of the signed-in user), `project-managed-identity`, or `agentic-identity`. With `oauth2` the first call returns `CONSENT_REQUIRED` (JSON-RPC code `-32006`) carrying a consent URL the user must visit once. |
| B · Voice Live direct | Voice Live itself | The `mcp` tool object takes `authorization` (a bearer token) and `headers` (arbitrary extra headers), alongside `allowed_tools` and `require_approval`. **The token is minted by you and handed to the service**, so it is only as good as your rotation story — and the server must be publicly reachable. |
| C · native AOAI Realtime | **Your backend** | No native MCP exists, so this is ordinary outbound HTTP from your own process. Use whatever the server expects — managed identity, a secret from Key Vault, OBO — and never let the credential near the model. |

The practical consequence: **A and C put the credential somewhere you control** (a
Foundry connection, or your own process). B is the only one where a long-lived
secret is handed to a Microsoft service to replay on your behalf. For a private
server, B degrades to C's model anyway — see the function-call proof above — which
means B's MCP auth story only matters for public servers.

### To RAG (AI Search or a vector store)

| Track | Who queries | How it authenticates |
|---|---|---|
| A · agent mode | Foundry Agent Service | A project connection to AI Search, using the project's **managed identity** (recommended) or an API key. `AzureAISearchTool` and File Search both go through it. In a network-isolated Standard setup, AI Search and File Search traffic is routed over **private endpoints**; the indexer additionally needs `executionEnvironment: "Private"`, or indexing silently succeeds and leaves you with an empty index. |
| B and C | **Your backend** | Your own Entra credential. This repo uses `AzureCliCredential` for local development; in production use a **managed identity** with `Search Index Data Reader` (or the vector store's equivalent) and let the search service refuse everything else at the network layer. |

Two things are worth stating plainly:

* In B and C the retrieval credential and the vector store id **never leave the
  backend**. The model only ever sees the text you chose to return. That is why the
  earlier client-side-search sketch was wrong.
* In A, retrieval privacy is achievable but expensive: it is the Standard setup, the
  BYO VNet, the /27 delegated subnet, and three private endpoints — and the empty-index
  trap above is a real one.

---

## Data residency: the three legs are governed differently

Run `python scripts/probe_data_residency.py` to audit a resource.

### The speech legs are fine, and explicitly so

> "Azure Speech doesn't store or process your data outside the region of your Azure
> Speech resource. The data is stored or processed only in the region where the
> resource is created."
> — [Supported regions for Azure Speech](https://learn.microsoft.com/azure/ai-services/speech-service/regions)

That covers **processing**, not just storage, so STT and TTS both stay in
`francecentral`. Voice Live adds that it "does not store or retain customer data";
opt-in debug logging (support tickets only) stays in the same region for 30 days.

France Central also *does* offer HD voices and MAI voices — the regions page's Text to
speech tab marks both ✅. An earlier draft of this document warned that
`en-US-Ava:DragonHDLatestNeural` was undocumented there, based on a narrower list in
the Voice Live how-to. That was wrong; the regions page is authoritative. Voice choice
in France Central is a free decision.

### The brain leg is where residency actually breaks

The deployment SKU decides, not the resource region:

```
GlobalStandard / GlobalProvisionedManaged / GlobalBatch  -> ANY Azure region
DataZoneStandard / DataZoneProvisionedManaged            -> within US / EU / APAC
Standard / ProvisionedManaged                            -> the deployment region
DeveloperTier                                            -> no residency guarantee
```

On `fdy-sa33b5nih2ogs`:

| Deployment | SKU | Processed in | EU-safe |
|---|---|---|---|
| `gpt-5` | GlobalStandard | any Azure region | **no** |
| `gpt-4o-mini` | GlobalStandard | any Azure region | **no** |
| `text-embedding-3-small` | GlobalStandard | any Azure region | **no** |
| `gpt-realtime-1.5` | DataZoneStandard | within the data zone | yes |
| `Mistral-Large-3` | DataZoneStandard | within the data zone | yes |

The agent in this repo runs on `gpt-5` — GlobalStandard — so despite a French resource
and in-region speech, the LLM leg may be processed anywhere. Fix by pointing the agent
at a Data Zone *chat* deployment (`Mistral-Large-3`, or redeploy `gpt-5` as
`DataZoneStandard`). `gpt-realtime-1.5` cannot back an agent.

### The trap: Voice Live's managed models are not all Data Zone

The regions page's Voice Live tab gives a deployment type **per model, per region**.
For `francecentral`:

| Voice Live managed model | Deployment type in francecentral |
|---|---|
| `gpt-realtime-1.5`, `gpt-realtime`, `gpt-realtime-mini`, `azure-realtime` | **Global standard** |
| `gpt-4o`, `gpt-4o-mini`, `gpt-4.1*`, `gpt-5`, `gpt-5-mini`, `gpt-5-nano`, `gpt-5.1` | Data zone standard |
| `gpt-5.4`, `gpt-5.2` | Global standard |

So if you use Voice Live's **managed** `gpt-realtime-1.5`, inference is Global
standard and can leave the EU — even though the identical model, deployed by you as
`DataZoneStandard`, does not. **BYOM is a residency decision here, not just a control
one.** The direct-mode track in this repo already routes through your own deployment,
which is exactly the right choice for an EU tender.

---

## Decision: the two shapes that satisfy EU residency AND private networking

Once you accept a Data Zone chat deployment for the agent, both shapes are viable:

* **A — Direct + BYOM + function calls.** Your backend holds the Voice Live socket,
  runs retrieval, and acts as the MCP client for any private MCP server.
* **B — Agent voice mode (Data Zone model) + native MCP on Standard private setup.**

| | A — Direct + BYOM + functions | B — Agent + private Standard |
|---|---|---|
| **Speech (STT/TTS)** | in resource region | in resource region |
| **LLM residency** | your `DataZoneStandard` deployment | your `DataZoneStandard` deployment |
| **Audio path** | **native speech-to-speech** | cascaded STT → LLM → TTS |
| **Turn latency** | **lowest achievable** | + STT and TTS hops |
| **Private RAG** | backend → private endpoint | File Search / AI Search → private endpoint |
| **Private MCP** | via backend function proxy (**proven**) | **native**, through your VNet subnet |
| **MCP tool discovery** | manual schemas | **automatic** |
| **MCP approval flow** | you build it | **built in** |
| **MCP turn completion** | **you must call `response.create()`** after `mcp_call.completed`, or the turn goes silent | **agent orchestrates the loop** and speaks the result |
| **Threads / tracing / versioning** | you build it | **built in** |
| **Interim "let me check…"** | unsupported on realtime | **supported** |
| **Infra to stand up** | Foundry + private endpoint + **one backend** | Standard setup: **BYO Storage + AI Search + Cosmos DB**, VNet, /27 delegated subnet, 3+ private endpoints, **and still a backend** |
| **Code you own** | retrieval + tool plumbing | session plumbing only |

### MCP through agent voice mode: verified

The FAQ's "supported ... further with Foundry (new) agents" is ambiguous — it could
mean the agent can use MCP via the Responses API, or that MCP still fires when the
agent is driven through Voice Live. `scripts/probe_agent_mcp.py` settles the second,
which is the one that matters for a voice product:

```
CONTROL - File Search through voice mode
  A: "It's due on the tenth of April twenty twenty-six at twelve noon CET."   PASS

TEST - MCP through voice mode
  [event] response.mcp_call.in_progress
  [event] response.mcp_call_arguments.done
  [event] response.mcp_call.completed
  A: "byom-azure-openai-realtime, byom-azure-openai-chat-completion, and
      byom-foundry-anthropic-messages"                                        PASS
```

Three things worth noting, two of which corrected earlier assumptions here:

1. It works on a **basic** agent setup — no VNet, no Standard setup. Private
   networking is only needed to make the MCP *server* private, not to use MCP.
2. The MCP lifecycle **is mirrored onto the Voice Live socket** as
   `response.mcp_call.*`, so tool activity can be surfaced in the UI for free. An
   earlier draft assumed these events stayed inside the Agent Service.
3. The client does **not** call `response.create()` afterwards. The agent orchestrates
   the tool loop and speaks the result — the exact opposite of direct-model mode,
   where omitting that call ends the turn in silence.

Point 3 is a real ergonomic advantage for B that the latency comparison alone hides.

### Three challenges to the framing

**1. Agent mode does not remove the backend.** Something still has to hold the Voice
Live WebSocket and serve the browser. Option B makes that process thinner; it does not
delete it. "Less infrastructure" is true of *tools*, not of *hosting*.

**2. Private MCP is not a differentiator.** B gets it natively; A gets it through a
backend proxy, demonstrated end-to-end in
`scripts/probe_private_mcp_via_function.py`. What B actually buys is *native MCP
semantics* — automatic discovery, the approval flow, service-side retries — not
network reach.

**3. Both options share an unverified inbound risk.** To make either fully private you
need a custom domain plus a private endpoint on the Foundry resource, with the backend
inside the VNet. `fdy-sa33b5nih2ogs` already has a custom domain, so the pattern
applies. But the Speech private-link guidance warns that passing an auth token in the
`Authorization` header works "only if you turned on the **All networks** access
option" — and Voice Live agent mode is **Entra-only**. Whether that restriction
applies to the Voice Live data plane is **not verified here**. It is a risk to both
shapes, so it does not pick a winner, but it must be tested on a throwaway resource
before either is promised.

### So which one?

The differentiators reduce to a single trade:

> **A trades managed retrieval and governance for latency and control.
> B trades latency for managed retrieval and governance.**

For this tender, **A**. Annex D asks for a first response under 1.5 s and a turn
latency under 1.2 s at the 95th percentile, and cascading recognition → generation →
synthesis adds two model hops to every turn. A native speech-to-speech path is the
only one with headroom, and P-03 is scored, not aspirational.

Choose **B** instead when the scored criteria are governance-shaped rather than
latency-shaped — auditability, approval trails, thread history, agent versioning — or
when nobody on the team wants to own a retrieval path.

Do not choose on infrastructure cost: B's Standard setup is a materially larger
commitment (BYO Storage, AI Search and Cosmos DB, VNet injection, a delegated subnet,
several private endpoints) than A's single backend behind a private endpoint.

**This is measurable, not a matter of taste.** Both tracks in this repo work today,
so the honest way to settle it is to time a turn on each against the P-02/P-03
targets and let the numbers decide.

---

## Reproducing

```bash
az login
python scripts/probe_voicelive_region.py     # region + credentials gate
python scripts/probe_voice_matrix.py         # voices and models this region accepts
python scripts/probe_model_control.py        # the eight experiments above
python scripts/probe_agent_session.py        # the agent-mode pipeline, field by field
python scripts/test_backend_turn.py          # a full BYOM turn, no microphone needed
```
